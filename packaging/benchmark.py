"""Measures each Hyper-Wispr feature on a Windows PC: how long it takes, how much memory and
processor it uses, and for speech how accurate it is.

    python packaging/benchmark.py --out bench-results

Run by .github/workflows/benchmark.yml on a GitHub Windows machine, which has no microphone and no
NVIDIA card. The speech is synthesized from known sentences, so accuracy can be scored, and the
real app gets it in place of the microphone. The sentences are made up: no patient text is used.
Writes results.json and summary.md to --out. The app itself never runs this.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import json
import logging
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hyper_wispr import config  # noqa: E402

RATE = 16000
CLIPS = {
    "short": "See you tomorrow.",
    "general": "Hi Sarah, I wanted to follow up on yesterday's meeting. Could you send me the updated schedule "
               "by Friday afternoon? Thanks again for your help.",
    "m1": "The patient is a 67 year old man with hypertension and atrial fibrillation on metoprolol "
          "25 milligrams twice daily.",
    "m2": "Start apixaban 5 milligrams twice a day and furosemide 40 milligrams intravenously. "
          "Blood pressure 132 over 84, heart rate 78.",
    "m3": "Subarachnoid hemorrhage, Hunt and Hess grade 3. Continue nimodipine 60 milligrams every 4 hours "
          "and levetiracetam 500 milligrams twice daily. Start a nicardipine infusion to keep the systolic "
          "pressure below 160.",
    "m4": "Sedated with propofol and dexmedetomidine. Ceftriaxone 2 grams daily. Norepinephrine at 0.08 "
          "micrograms per kilogram per minute. Lactate is 2.1.",
    "m5": "Abdomen is soft and non tender. No hepatosplenomegaly. The chest X-ray shows bilateral pleural "
          "effusions and cardiomegaly.",
    "m6": "Metoprolol no longer needed. Levetiracetam no change. Hold heparin tonight.",
    "command": "Make this shorter.",
}
MEDICAL = ["m1", "m2", "m3", "m4", "m5", "m6"]
AI_CLIPS = ["general_0"] + [f"{k}_0" for k in MEDICAL]
# The medical words each sentence has, spelled as they should be typed.
TERMS = {
    "m1": ["hypertension", "atrial fibrillation", "metoprolol"],
    "m2": ["apixaban", "furosemide"],
    "m3": ["subarachnoid", "hemorrhage", "Hunt and Hess", "nimodipine", "levetiracetam", "nicardipine"],
    "m4": ["propofol", "dexmedetomidine", "ceftriaxone", "norepinephrine"],
    "m5": ["hepatosplenomegaly", "pleural effusions", "cardiomegaly"],
    "m6": ["metoprolol no longer", "levetiracetam no change", "heparin"],
}
# How the doses should come out of the app's medical writing rules.
WRITTEN = {
    "m1": ["25 mg"], "m2": ["5 mg", "40 mg", "132/84"], "m3": ["60 mg", "500 mg"],
    "m4": ["2 g", "0.08 mcg/kg/min"], "m5": [], "m6": [],
}
VOICES = ["en-US-AriaNeural", "en-GB-RyanNeural"]

R: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
OUT = Path("bench-results")
log = logging.getLogger("benchmark")


def save() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(R, indent=1, default=str), encoding="utf-8")


def section(name):
    """Runs one part of the benchmark; a failure is recorded and the next part still runs."""

    def deco(fn):
        def run(*a, **k):
            print(f"\n===== {name} =====", flush=True)
            t = time.perf_counter()
            R.setdefault(name, {})
            out = None
            try:
                out = fn(R[name], *a, **k)
            except Exception as e:
                R[name]["error"] = f"{type(e).__name__}: {e}"
                R[name]["trace"] = traceback.format_exc()[-3000:]
                traceback.print_exc()
            R[name]["section_seconds"] = round(time.perf_counter() - t, 1)
            print(json.dumps(R[name], default=str)[:4000], flush=True)
            save()
            return out

        return run

    return deco


def settings(**over) -> dict:
    s = copy.deepcopy(config.DEFAULTS)
    s["speech"]["device"] = "cpu"
    s["ai_cleanup"]["enabled"] = False
    s["audio"]["sounds"] = False
    for key, value in over.items():
        part, _, name = key.partition("__")
        s[part][name] = value
    return s


# ---- measuring ----------------------------------------------------------------------


class Meter:
    """Wall time, processor time and peak memory of some processes while a block runs.
    machine: also the whole computer's, for work done in processes we don't know (Ollama's)."""

    def __init__(self, procs=None, every: float = 0.1, machine: bool = False):
        self.procs = procs or (lambda: [psutil.Process()])
        self.every = every
        self.machine = machine

    def _poll(self, first: bool = False) -> None:
        rss = 0
        for p in self.procs():
            try:
                with p.oneshot():
                    c = p.cpu_times()
                    rss += p.memory_info().rss
            except psutil.Error:
                continue
            total = c.user + c.system
            self.base.setdefault(p.pid, total if first else 0.0)
            self.last[p.pid] = total
        self.peak = max(self.peak, rss)
        self.rss = rss
        if self.machine:
            self.machine_peak = max(getattr(self, "machine_peak", 0), psutil.virtual_memory().used)

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            self._poll()

    def __enter__(self):
        self.base, self.last, self.peak, self.rss = {}, {}, 0, 0
        if self.machine:
            self.machine_start, self.machine_peak = psutil.virtual_memory().used, 0
            self.cpu_start = psutil.cpu_times()
        self._poll(first=True)
        self.start_rss = self.rss
        self.t0 = time.perf_counter()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.t0
        self._stop.set()
        self._thread.join()
        self._poll()
        self.cpu_seconds = sum(self.last[p] - self.base.get(p, 0.0) for p in self.last)
        if self.machine:
            a, b = self.cpu_start, psutil.cpu_times()
            busy = (sum(b) - b.idle) - (sum(a) - a.idle)
            self.machine_cores = busy / self.seconds if self.seconds else 0
        return False

    def dict(self) -> dict:
        out = {
            "seconds": round(self.seconds, 3),
            "cores_busy": round(self.cpu_seconds / self.seconds, 2) if self.seconds else 0,
            "peak_mb": round(self.peak / 2**20),
            "end_mb": round(self.rss / 2**20),
        }
        if self.machine:
            out["machine_cores_busy"] = round(self.machine_cores, 2)
            out["machine_mb_added_peak"] = round((self.machine_peak - self.machine_start) / 2**20)
        return out


def mb(proc=None) -> float:
    return round((proc or psutil.Process()).memory_info().rss / 2**20, 1)


def tree(pid: int) -> list:
    try:
        p = psutil.Process(pid)
        return [p] + p.children(recursive=True)
    except psutil.Error:
        return []


def ollama_procs() -> list:
    out = []
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower().startswith("ollama"):
            out.append(p)
    return out


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


# ---- scoring the text -------------------------------------------------------------

_SAME = [
    (r"\bmilligrams?\b", "mg"), (r"\bmicrograms?\b", "mcg"), (r"\bkilograms?\b", "kg"), (r"\bgrams?\b", "g"),
    (r"\bx[- ]ray\b", "xray"), (r"\bnon[- ]", "non"), (r"(\d+)\s*/\s*(\d+)", r"\1 over \2"),
    (r"(\d+)-year-old", r"\1 year old"), (r"mg/kg/min|mcg/kg/min", "mcg per kg per min"),
    (r"\bminute\b", "min"), (r"\bper kg\b", "per kg"),
]


def norm(text: str) -> list[str]:
    t = " " + text.lower() + " "
    for a, b in _SAME:
        t = re.sub(a, b, t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)
    t = re.sub(r"[^\w\s./]", " ", t).replace("-", " ")
    return t.split()


def wer(ref: str, hyp: str) -> float:
    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return round(d[len(h)] / max(1, len(r)), 3)


def terms_found(clip: str, text: str) -> list[str]:
    """The expected medical words missing (or misspelled) in the text."""
    low = " " + " ".join(norm(text)) + " "
    return [t for t in TERMS.get(clip.split("_")[0], []) if " " + " ".join(norm(t)) + " " not in low]


def written_missing(clip: str, text: str) -> list[str]:
    return [w for w in WRITTEN.get(clip.split("_")[0], []) if w not in text]


# ---- the test audio ----------------------------------------------------------------


def _sapi(text: str, path: Path) -> None:
    script = (
        "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
        "$s.SetOutputToWaveFile($env:HW_WAV, $f); $s.Speak($env:HW_TEXT); $s.Dispose()"
    )
    env = dict(os.environ, HW_WAV=str(path), HW_TEXT=text)
    subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env, check=True, timeout=120)


def _pink(n: int, rng) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(len(spectrum))
    spectrum[1:] /= np.sqrt(f[1:])
    noise = np.fft.irfft(spectrum, n)
    return (noise / (np.sqrt(np.mean(noise**2)) + 1e-12)).astype(np.float32)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


def decode_audio(path: str, sampling_rate: int = RATE) -> np.ndarray:
    """An audio file as 16 kHz mono floats. faster-whisper has one, but it passes PyAV an argument
    that PyAV 19 no longer takes (the app never decodes files, so only this script needs one)."""
    import av

    chunks = []
    with av.open(path) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray() for f in resampler.resample(frame)]
        chunks += [f.to_ndarray() for f in resampler.resample(None)]
    return (np.concatenate(chunks, axis=1).reshape(-1) / 32768.0).astype(np.float32)


@section("audio")
def make_audio(res: dict, folder: Path) -> dict[str, np.ndarray]:
    folder.mkdir(parents=True, exist_ok=True)
    clips: dict[str, np.ndarray] = {}
    engine = "edge-tts"
    for key, text in CLIPS.items():
        voices = VOICES if key in MEDICAL else VOICES[:1]
        for i, voice in enumerate(voices):
            name = f"{key}_{i}"
            path = folder / f"{name}.mp3"
            if engine == "edge-tts":
                try:
                    import edge_tts

                    asyncio.run(edge_tts.Communicate(text, voice).save(str(path)))
                except Exception as e:
                    res["edge_tts_error"] = f"{type(e).__name__}: {e}"
                    engine = "sapi"
            if engine == "sapi":
                if i:
                    continue  # one voice only
                path = folder / f"{name}.wav"
                _sapi(text, path)
            clips[name] = decode_audio(str(path), sampling_rate=RATE)
    rng = np.random.default_rng(7)
    gap = np.zeros(RATE // 2, dtype=np.float32)
    clips["long_0"] = np.concatenate([x for k in ["m1_0", "m2_0", "m3_0", "m4_0", "m5_0", "general_0"] for x in (clips[k], gap)])
    for k in MEDICAL:
        c = clips[f"{k}_0"]
        clips[f"{k}_noisy"] = c + _pink(len(c), rng) * (_rms(c) / 10 ** (15 / 20))  # 15 dB below the voice
    c = clips["m1_0"]
    clips["m1_quiet"] = c * (0.004 / _rms(c)) + _pink(len(c), rng) * 0.0004  # soft speech, a far microphone
    clips["silence"] = np.zeros(RATE * 10, dtype=np.float32)
    clips["noise"] = _pink(RATE * 10, rng) * 0.01
    res["engine"] = engine
    res["clips"] = {k: round(len(v) / RATE, 1) for k, v in clips.items()}
    return clips


def reference(name: str) -> str:
    key = name.split("_")[0]
    if key == "long":
        return " ".join(CLIPS[k] for k in ["m1", "m2", "m3", "m4", "m5", "general"])
    return CLIPS.get(key, "")


# ---- the parts ---------------------------------------------------------------------


@section("environment")
def environment(res: dict) -> None:
    import ctranslate2
    import faster_whisper

    res["os"] = platform.platform()
    res["python"] = sys.version.split()[0]
    res["cpu"] = platform.processor()
    if sys.platform == "win32":
        try:
            res["cpu"] = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                                        capture_output=True, text=True, timeout=60).stdout.strip() or res["cpu"]
        except Exception:
            pass
    res["cores_logical"] = psutil.cpu_count()
    res["cores_physical"] = psutil.cpu_count(logical=False)
    res["ram_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    res["faster_whisper"] = faster_whisper.__version__
    res["ctranslate2"] = ctranslate2.__version__
    res["ct2_cpu_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cpu"))
    from hyper_wispr import gpu_pack

    res["nvidia_cards"] = gpu_pack.nvidia_cards()


@section("medical")
def bench_medical(res: dict) -> None:
    from hyper_wispr import cleanup, medical

    s = settings()
    before = mb()
    t = time.perf_counter()
    medical.english_words()
    res["english_list_s"] = round(time.perf_counter() - t, 3)
    t = time.perf_counter()
    lex = medical.lexicon(s)
    res["dictionary_build_s"] = round(time.perf_counter() - t, 3)
    res["dictionary_terms"] = len(lex.terms)
    res["memory_mb"] = round(mb() - before, 1)
    texts = [CLIPS[k] for k in MEDICAL] + [CLIPS["general"]]
    times = []
    for _ in range(20):
        for text in texts:
            t = time.perf_counter()
            medical.fix(cleanup.clean(text, s), s)
            times.append(time.perf_counter() - t)
    res["fix_ms_median"] = round(1000 * median(times), 2)
    res["fix_ms_max"] = round(1000 * max(times), 2)
    t = time.perf_counter()
    for _ in range(20):
        medical.speech_prompt(s)
    res["speech_prompt_ms"] = round(1000 * (time.perf_counter() - t) / 20, 2)
    t = time.perf_counter()
    for _ in range(20):
        medical.ai_terms(CLIPS["m3"], s)
    res["ai_terms_ms"] = round(1000 * (time.perf_counter() - t) / 20, 2)
    t = time.perf_counter()
    for q in ("metop", "levetir", "apixa", "hepato"):
        medical.lookup(q, s)
    res["lookup_ms"] = round(1000 * (time.perf_counter() - t) / 4, 2)
    every = settings(medical__specialties=list(medical.SPECIALTIES))
    t = time.perf_counter()
    res["all_specialties_terms"] = len(medical.lexicon(every).terms)
    res["all_specialties_build_s"] = round(time.perf_counter() - t, 3)
    res["examples"] = {CLIPS[k]: medical.fix(cleanup.clean(CLIPS[k], s), s) for k in MEDICAL}


@section("history")
def bench_history(res: dict, folder: Path) -> None:
    from hyper_wispr.history import History

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "history.sqlite3"
    h = History(path)
    rng = np.random.default_rng(3)
    audio = (rng.standard_normal(RATE * 8) * 0.05).astype(np.float32)  # an 8-second dictation
    text = CLIPS["m3"]
    times = []
    for _ in range(300):
        t = time.perf_counter()
        recorded = h.add("WINWORD.EXE", "document", 8.0, text, text, 1.5, audio=audio, keep_recordings=20)
        times.append(time.perf_counter() - t)
    res["add_with_recording_ms"] = round(1000 * median(times), 1)
    t = time.perf_counter()
    for _ in range(2700):
        h.add("OUTLOOK.EXE", "email", 6.0, CLIPS["general"], CLIPS["general"], 1.0)
    res["add_text_only_ms"] = round(1000 * (time.perf_counter() - t) / 2700, 2)
    res["rows"] = 3000
    for label, search in (("recent_200_ms", ""), ("search_ms", "nimodipine"), ("search_again_ms", "nimodipine hours"),
                          ("search_none_ms", "zzzz")):
        t = time.perf_counter()
        h.recent(200, search=search)
        res[label] = round(1000 * (time.perf_counter() - t), 1)
    t = time.perf_counter()
    h.daily()
    res["dashboard_totals_ms"] = round(1000 * (time.perf_counter() - t), 1)
    res["file_mb"] = round(path.stat().st_size / 2**20, 2)
    t = time.perf_counter()
    res["recording_kept"] = h.recording(recorded) is not None
    res["load_recording_ms"] = round(1000 * (time.perf_counter() - t), 1)
    t = time.perf_counter()
    h.delete(recorded)
    res["delete_one_ms"] = round(1000 * (time.perf_counter() - t), 1)
    res["file_mb_after_delete"] = round(path.stat().st_size / 2**20, 2)
    h.close()


def _transcribe_rows(tr, clips, names, s, extra=None):
    from hyper_wispr.app import process_text

    rows = []
    for name in names:
        audio = clips[name]
        with Meter() as m:
            raw = tr.transcribe(audio)
        final = process_text(raw, s, "document")
        ref = reference(name)
        row = {"clip": name, "audio_s": round(len(audio) / RATE, 1), **m.dict(),
               "speed": round(len(audio) / RATE / m.seconds, 1) if m.seconds else None,
               "raw": raw, "final": final}
        if ref:
            row["wer"] = wer(ref, raw)
            row["terms_missing_raw"] = terms_found(name, raw)
            row["terms_missing_final"] = terms_found(name, final)
            row["doses_missing_final"] = written_missing(name, final)
        row.update(extra or {})
        rows.append(row)
        print(f"  {name}: {m.seconds:.2f}s for {row['audio_s']}s, wer {row.get('wer')}, final: {final[:120]}", flush=True)
    return rows


@section("speech_small")
def bench_speech(res: dict, clips: dict) -> dict:
    """The default speech model on a PC without a usable NVIDIA card, through the app's Transcriber."""
    from hyper_wispr import speech

    s = settings()
    t = time.perf_counter()
    speech.download("small")
    res["download_s"] = round(time.perf_counter() - t, 1)
    gc.collect()
    before = mb()
    with Meter() as m:
        tr = speech.Transcriber(s)
    res["model"] = tr.description
    res["load"] = m.dict()
    res["memory_mb"] = round(mb() - before)
    names = [f"{k}_{i}" for k in MEDICAL for i in range(2) if f"{k}_{i}" in clips]
    rows = _transcribe_rows(tr, clips, ["short_0", "general_0"] + names, s, {"medical": True})
    s["medical"]["enabled"] = False
    rows += _transcribe_rows(tr, clips, names, s, {"medical": False})
    s["medical"]["enabled"] = True
    rows += _transcribe_rows(tr, clips, [f"{k}_noisy" for k in MEDICAL], s, {"medical": True, "noisy": True})
    rows += _transcribe_rows(tr, clips, ["long_0"], s, {"medical": True})
    s["speech"]["beam_size"] = 1
    rows += _transcribe_rows(tr, clips, [f"{k}_0" for k in MEDICAL], s, {"medical": True, "beam": 1})
    s["speech"]["beam_size"] = 5
    s["speech"]["languages"] = ["en", "ar"]
    rows += _transcribe_rows(tr, clips, [f"{k}_0" for k in MEDICAL[:3]], s, {"medical": True, "languages": "en+ar"})
    s["speech"]["languages"] = ["en"]
    rows += _transcribe_rows(tr, clips, ["m1_quiet"], s, {"whisper_mode": False})
    s["audio"]["whisper_mode"] = True
    rows += _transcribe_rows(tr, clips, ["m1_quiet"], s, {"whisper_mode": True})
    s["audio"]["whisper_mode"] = False
    rows += _transcribe_rows(tr, clips, ["silence", "noise"], s, {"phantom_check": True})
    res["rows"] = rows
    res["memory_after_mb"] = mb()
    out = {r["clip"]: r["final"] for r in rows if r["clip"] in AI_CLIPS and r.get("medical") and "beam" not in r
           and "languages" not in r and "noisy" not in r}
    del tr
    gc.collect()
    return out


@section("speech_turbo")
def bench_turbo(res: dict, clips: dict) -> None:
    """The bigger model used by History's Redo (and by "auto" on a usable NVIDIA card), on the processor."""
    from hyper_wispr import speech

    s = settings()
    t = time.perf_counter()
    speech.download("large-v3-turbo")
    res["download_s"] = round(time.perf_counter() - t, 1)
    gc.collect()
    before = mb()
    with Meter() as m:
        tr = speech.Transcriber(s, model_override="large-v3-turbo")
    res["model"] = tr.description
    res["load"] = m.dict()
    res["memory_mb"] = round(mb() - before)
    res["rows"] = _transcribe_rows(tr, clips, [f"{k}_0" for k in MEDICAL] + ["m3_noisy", "long_0"], s, {"medical": True})
    del tr
    gc.collect()


class _Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.seen: list[str] = []

    def emit(self, record):
        self.seen.append(record.getMessage())


# The AI instructions before this audit, rebuilt from today's: the medical rule's example ("5 mg PO
# BID") was copied into dictations that never said it, and nothing asked for plain text.
OLD_PROMPT = [
    ("\n- Plain text only: no markdown, no asterisks, no bold.", ""),
    ("Keep every number, dose, unit, frequency and route as spoken. Never add a route, frequency or unit that wasn't said.",
     'Keep every number, dose, unit, frequency and route exactly as spoken (e.g. "5 mg PO BID").'),
]


def _ai_checks(said: str, out: str) -> dict:
    from hyper_wispr import ai_cleanup

    return {"markdown": "**" in out or "  \n" in out, "added_route": ai_cleanup.adds_route(said, out),
            "lines_added": out.count("\n") - said.count("\n"), "numbers_kept": ai_cleanup.numbers_kept(said, out, exact=True),
            "meaning_kept": ai_cleanup.keeps_meaning(said, out, settings())}


def _ollama_snapshot() -> list:
    out = []
    for p in psutil.process_iter(["name", "pid", "ppid", "memory_info"]):
        name = (p.info["name"] or "").lower()
        rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
        if "llama" in name or "ollama" in name or rss > 400 * 2**20:
            out.append({"name": p.info["name"], "pid": p.info["pid"], "ppid": p.info["ppid"], "mb": round(rss / 2**20)})
    return out


@section("ai_cleanup")
def bench_ai(res: dict, texts: dict) -> None:
    from hyper_wispr import ai_cleanup, cleanup, medical

    s = settings(ai_cleanup__enabled=True, ai_cleanup__engine="ollama")
    if not ai_cleanup.is_available(s):
        raise RuntimeError("Ollama isn't running")
    base = s["ai_cleanup"]["ollama_url"].rstrip("/")
    model = s["ai_cleanup"]["model"]
    res["model"] = model
    # What the app hands the AI: the transcript after the basic cleanup and the medical fixes.
    inputs = {name: cleanup.apply_style(medical.fix(cleanup.clean(text, s), s), "formal") for name, text in texts.items()}
    inputs["long"] = cleanup.apply_style(medical.fix(cleanup.clean(" ".join(CLIPS[k] for k in MEDICAL), s), s), "formal")
    res["server_idle_mb"] = round(sum(mb(p) for p in ollama_procs()))
    with Meter(ollama_procs, machine=True) as m:
        ai_cleanup.warm_up(s, "document")
    res["warm_up"] = m.dict()
    res["processes_loaded"] = _ollama_snapshot()
    catcher = _Warnings()
    logging.getLogger("hyper_wispr").addHandler(catcher)
    rows = []
    for name, text in inputs.items():
        if not text or len(text.split()) < s["ai_cleanup"]["min_words"]:
            continue
        catcher.seen.clear()
        with Meter(ollama_procs, machine=True) as m:
            out = ai_cleanup.refine(text, s, "document")
        row = {"clip": name, "words": len(text.split()), "app": m.dict(), "app_used_ai": not catcher.seen,
               "app_warnings": list(catcher.seen), "input": text, "app_output": out}
        # The same dictation without the app's checks or time limit, with today's and the old instructions.
        new_system = ai_cleanup.build_system_prompt(s, "document", "", text)
        old_system = new_system
        for a, b in OLD_PROMPT:
            old_system = old_system.replace(a, b)
        for label, system in (("new", new_system), ("old", old_system)):
            payload = {"model": model, "stream": False, "keep_alive": "30m", "options": {"temperature": 0},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": f"<dictation>\n{text}\n</dictation>"}]}
            t = time.perf_counter()
            r = ai_cleanup._post(base + "/api/chat", payload, 600)
            content = r["message"]["content"].strip().removeprefix("<dictation>").removesuffix("</dictation>").strip()
            row[label] = {"seconds": round(time.perf_counter() - t, 2), "output_tokens": r.get("eval_count"),
                          "output_s": round(r.get("eval_duration", 0) / 1e9, 2),
                          "prompt_tokens": r.get("prompt_eval_count"), "prompt_s": round(r.get("prompt_eval_duration", 0) / 1e9, 2),
                          "output": content, **_ai_checks(text, content)}
        row["app_checks"] = _ai_checks(text, out)
        rows.append(row)
        print(f"  {name}: app {row['app']['seconds']}s (AI used: {row['app_used_ai']}); "
              f"new {row['new']['seconds']}s {row['new']['output']!r}; old {row['old']['output']!r}", flush=True)
    res["rows"] = rows
    out_s = sum(r["new"]["output_s"] for r in rows)
    res["words_per_second"] = round(sum(len(r["new"]["output"].split()) for r in rows) / out_s, 1) if out_s else None
    with Meter(ollama_procs, machine=True) as m:
        try:
            out = ai_cleanup.command("make this shorter", CLIPS["m3"], s)
        except ai_cleanup.CommandError as e:
            out = f"refused: {e}"
    res["command"] = {**m.dict(), "output": out}

    # After 30 minutes without a dictation Ollama lets the model go. The first dictation then:
    def let_go() -> None:
        ai_cleanup._post(base + "/api/generate", {"model": model, "keep_alive": 0}, 60)
        time.sleep(3)
        ai_cleanup._last_answer = ai_cleanup._last_warm_up = float("-inf")

    first = inputs.get("m1_0") or CLIPS["m1"]
    let_go()
    catcher.seen.clear()
    with Meter(ollama_procs, machine=True) as m:
        ai_cleanup.refine(first, s, "document")
    res["after_idle_no_warm_up"] = {**m.dict(), "used_ai": not catcher.seen, "warnings": list(catcher.seen)}
    # ... and as the app does it now: loading starts when the keys go down, while the speaker talks.
    let_go()
    catcher.seen.clear()
    ai_cleanup.prewarm(s, "document")
    time.sleep(6)  # talking, then the speech model
    with Meter(ollama_procs, machine=True) as m:
        ai_cleanup.refine(first, s, "document")
    res["after_idle_with_warm_up"] = {**m.dict(), "used_ai": not catcher.seen, "warnings": list(catcher.seen)}
    logging.getLogger("hyper_wispr").removeHandler(catcher)
    ai_cleanup.warm_up(s)


def ai_helper_procs() -> list:
    """The built-in AI engine's helper process (local_llm), started by this process."""
    out = []
    for p in psutil.Process().children(recursive=True):
        try:
            if "--ai-worker" in p.cmdline():
                out.append(p)
        except psutil.Error:
            pass
    return out


@section("ai_builtin")
def bench_ai_builtin(res: dict, texts: dict) -> None:
    """The built-in engine (llama.cpp in a helper process): each model, guessing ahead from the
    dictation on and off, on the same dictations as the Ollama part, through the app's own checks."""
    from hyper_wispr import ai_cleanup, cleanup, local_llm, medical

    if not local_llm.usable():
        raise RuntimeError("built-in engine not usable: " + local_llm.why_not())
    base = settings(ai_cleanup__enabled=True)
    inputs = {name: cleanup.apply_style(medical.fix(cleanup.clean(text, base), base), "formal") for name, text in texts.items()}
    inputs["long"] = cleanup.apply_style(medical.fix(cleanup.clean(" ".join(CLIPS[k] for k in MEDICAL), base), base), "formal")
    catcher = _Warnings()
    logging.getLogger("hyper_wispr").addHandler(catcher)
    stats: list = []

    class _Stats(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("AI cleanup (built in):"):
                stats.append(record.getMessage().split(":", 1)[1].strip())

    logging.getLogger("hyper_wispr.local_llm").addHandler(_Stats())
    runs = []
    for model in local_llm.MODELS:
        t = time.perf_counter()
        local_llm.download(model)
        dl = round(time.perf_counter() - t, 1)
        for draft in (0, 8):
            s = settings(ai_cleanup__enabled=True, ai_cleanup__engine="builtin", ai_cleanup__builtin_model=model,
                         ai_cleanup__draft_tokens=draft)
            local_llm.close()  # a fresh helper: loading is measured
            ai_cleanup._last_answer = ai_cleanup._last_warm_up = float("-inf")
            with Meter(ai_helper_procs, machine=True) as m:
                ai_cleanup.warm_up(s, "document")
            run = {"model": model, "draft": draft, "download_s": dl, "warm_up": m.dict(), "rows": []}
            for name, text in inputs.items():
                if not text or len(text.split()) < s["ai_cleanup"]["min_words"]:
                    continue
                catcher.seen.clear()
                stats.clear()
                with Meter(ai_helper_procs, machine=True) as m:
                    out = ai_cleanup.refine(text, s, "document")
                row = {"clip": name, "words": len(text.split()), "app": m.dict(), "used_ai": not catcher.seen,
                       "warnings": list(catcher.seen), "engine": stats[-1] if stats else None, "output": out,
                       **_ai_checks(text, out)}
                # The same dictation without the app's time limit, to see the model's own answer and speed.
                if catcher.seen:
                    stats.clear()
                    t = time.perf_counter()
                    try:
                        raw = ai_cleanup._chat(s, ai_cleanup.build_system_prompt(s, "document", "", text),
                                               f"<dictation>\n{text}\n</dictation>", 600, 600, source=text)
                    except Exception as e:
                        raw = f"failed: {e}"
                    row["no_limit"] = {"seconds": round(time.perf_counter() - t, 2), "output": raw,
                                       "engine": stats[-1] if stats else None, **_ai_checks(text, raw)}
                run["rows"].append(row)
                print(f"  {model} draft {draft} {name}: {row['app']['seconds']}s AI used {row['used_ai']} "
                      f"{row['engine']} {out!r}", flush=True)
            with Meter(ai_helper_procs, machine=True) as m:
                try:
                    out = ai_cleanup.command("make this shorter", CLIPS["m3"], s)
                except ai_cleanup.CommandError as e:
                    out = f"refused: {e}"
            run["command"] = {**m.dict(), "output": out}
            runs.append(run)
            res["runs"] = runs
            save()
    local_llm.close()
    logging.getLogger("hyper_wispr").removeHandler(catcher)


@section("overlay")
def bench_overlay(res: dict) -> None:
    from hyper_wispr import overlay

    t = time.perf_counter()
    for i in range(100):
        overlay.render_pill(overlay.RECORDING, i / 30, 0.5, 1.0)
    res["frame_ms"] = round(10 * (time.perf_counter() - t), 2)
    ov = overlay.Overlay(level=lambda: 0.03 + 0.02 * math.sin(time.time() * 7))
    for state in (overlay.RECORDING, overlay.HANDS_FREE, overlay.WORKING):
        ov.show(state)
        time.sleep(1.0)
        with Meter() as m:
            time.sleep(5)
        res[state] = m.dict()
    ov.hide()
    time.sleep(1.0)
    with Meter() as m:
        time.sleep(5)
    res["hidden"] = m.dict()


# ---- the Windows desktop ------------------------------------------------------------

KEYUP, EXTENDED = 0x2, 0x1
LCTRL, LWIN, LALT, TAB, ESC = 0xA2, 0x5B, 0xA4, 0x09, 0x1B
WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH, EM_SETSEL = 0x000C, 0x000D, 0x000E, 0x00B1


class Desktop:
    """The few Windows calls the desktop tests need: windows, keys, text in Notepad."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        self.ct = ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.FindWindowExW.argtypes = (w.HWND, w.HWND, w.LPCWSTR, w.LPCWSTR)
        u.FindWindowExW.restype = w.HWND
        u.SendMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        u.SendMessageW.restype = w.LPARAM
        u.GetForegroundWindow.restype = w.HWND
        u.SetForegroundWindow.argtypes = (w.HWND,)
        u.BringWindowToTop.argtypes = (w.HWND,)
        u.ShowWindow.argtypes = (w.HWND, ctypes.c_int)
        u.SetFocus.argtypes = (w.HWND,)
        u.GetWindowThreadProcessId.argtypes = (w.HWND, ctypes.POINTER(w.DWORD))
        u.GetWindowThreadProcessId.restype = w.DWORD
        u.AttachThreadInput.argtypes = (w.DWORD, w.DWORD, w.BOOL)
        u.keybd_event.argtypes = (w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t)
        u.MapVirtualKeyW.argtypes = (w.UINT, w.UINT)
        u.GetClassNameW.argtypes = (w.HWND, w.LPWSTR, ctypes.c_int)
        u.IsWindowVisible.argtypes = (w.HWND,)
        u.LoadKeyboardLayoutW.argtypes = (w.LPCWSTR, w.UINT)
        u.LoadKeyboardLayoutW.restype = ctypes.c_void_p
        u.ActivateKeyboardLayout.argtypes = (ctypes.c_void_p, w.UINT)
        u.ActivateKeyboardLayout.restype = ctypes.c_void_p
        u.GetKeyboardLayout.argtypes = (w.DWORD,)
        u.GetKeyboardLayout.restype = ctypes.c_void_p
        u.VkKeyScanW.argtypes = (w.WCHAR,)
        u.VkKeyScanW.restype = ctypes.c_short
        self.EnumProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        u.EnumWindows.argtypes = (self.EnumProc, w.LPARAM)
        u.EnumChildWindows.argtypes = (w.HWND, self.EnumProc, w.LPARAM)
        self.k32 = ctypes.WinDLL("kernel32")
        self.k32.GetCurrentThreadId.restype = w.DWORD
        self.u, self.w = u, w

    def class_name(self, hwnd) -> str:
        buf = self.ct.create_unicode_buffer(256)
        self.u.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def pid_of(self, hwnd) -> int:
        pid = self.w.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        return pid.value

    def _enum(self, parent=None) -> list:
        found = []
        cb = self.EnumProc(lambda h, _l: found.append(h) or True)
        if parent is None:
            self.u.EnumWindows(cb, 0)
        else:
            self.u.EnumChildWindows(parent, cb, 0)
        return found

    def notepad(self, pid: int | None, timeout: float = 20):
        """(window, its text box) of the Notepad started as `pid` (or, failing that, any Notepad)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            late = time.time() > deadline - timeout / 2  # the Store Notepad runs under another process
            for h in self._enum():
                if self.class_name(h) == "Notepad" and self.u.IsWindowVisible(h) and (late or self.pid_of(h) == pid):
                    for c in self._enum(h):
                        if self.class_name(c) in ("Edit", "RichEditD2DPT"):
                            return h, c
            time.sleep(0.2)
        raise RuntimeError("Notepad's window didn't appear")

    def text(self, edit) -> str:
        n = self.u.SendMessageW(edit, WM_GETTEXTLENGTH, 0, 0)
        buf = self.ct.create_unicode_buffer(n + 1)
        self.u.SendMessageW(edit, WM_GETTEXT, n + 1, self.ct.addressof(buf))
        return buf.value

    def set_text(self, edit, text: str, select_all: bool = False) -> None:
        buf = self.ct.create_unicode_buffer(text)
        self.u.SendMessageW(edit, WM_SETTEXT, 0, self.ct.addressof(buf))
        end = len(text)
        self.u.SendMessageW(edit, EM_SETSEL, 0 if select_all else end, end)

    def key(self, vk: int, up: bool = False) -> None:
        flags = (KEYUP if up else 0) | (EXTENDED if vk in (0x5B, 0x5C) else 0)
        self.u.keybd_event(vk, self.u.MapVirtualKeyW(vk, 0), flags, 0)

    def press(self, *vks: int, hold: float = 0.05) -> None:
        for vk in vks:
            self.key(vk)
            time.sleep(0.02)
        time.sleep(hold)
        for vk in reversed(vks):
            self.key(vk, up=True)
            time.sleep(0.02)

    def focus(self, hwnd) -> bool:
        if self.u.GetForegroundWindow() == hwnd:
            return True
        me = self.k32.GetCurrentThreadId()
        fg = self.u.GetForegroundWindow()
        other = self.u.GetWindowThreadProcessId(fg, None) if fg else 0
        if other and other != me:
            self.u.AttachThreadInput(me, other, True)
        self.u.ShowWindow(hwnd, 9)  # SW_RESTORE
        self.u.BringWindowToTop(hwnd)
        self.u.SetForegroundWindow(hwnd)
        if other and other != me:
            self.u.AttachThreadInput(me, other, False)
        time.sleep(0.2)
        if self.u.GetForegroundWindow() != hwnd:
            # Windows lets the process that sent the last key move the focus.
            self.key(LALT)
            self.u.SetForegroundWindow(hwnd)
            self.key(0xE8)  # a key that does nothing, so letting go of Alt doesn't open the menu
            self.key(0xE8, up=True)
            self.key(LALT, up=True)
            time.sleep(0.2)
        return self.u.GetForegroundWindow() == hwnd

    def wait_text(self, edit, old: str, timeout: float = 60, settle: float = 0.6):
        """Waits for the text box to change, then to stop changing. (text, seconds until it changed)."""
        t0 = time.perf_counter()
        first = None
        last, last_change = old, t0
        while time.perf_counter() - t0 < timeout:
            now = self.text(edit)
            if now != last:
                last, last_change = now, time.perf_counter()
                if first is None:
                    first = last_change - t0
            elif first is not None and time.perf_counter() - last_change >= settle:
                break
            time.sleep(0.02)
        return last, first


@section("paste")
def bench_paste(res: dict, desk: Desktop, notepad) -> None:
    from hyper_wispr import clipboard, paste

    top, edit = notepad
    s = settings()
    text = "Start apixaban 5 mg twice a day."
    res["focused"] = desk.focus(top)
    clipboard.set_text("what the user had copied")
    desk.set_text(edit, "")
    t = time.perf_counter()
    paste.insert(text, s, "notepad.exe")
    typed, first = desk.wait_text(edit, "", timeout=10)
    res["typed_ok"] = typed.strip() == text
    res["seconds_until_typed"] = round(first, 3) if first is not None else None
    res["total_s"] = round(time.perf_counter() - t, 3)
    time.sleep(paste.RESTORE_DELAY + 0.8)
    res["clipboard_restored"] = clipboard.get_text() == "what the user had copied"
    # An Arabic keyboard layout: there's no "v" key to type, which is how Ctrl+V used to be sent.
    us = desk.u.GetKeyboardLayout(0)
    arabic = desk.u.LoadKeyboardLayoutW("00000401", 1)  # KLF_ACTIVATE
    res["arabic_layout_loaded"] = bool(arabic)
    res["arabic_has_no_v_key"] = desk.u.VkKeyScanW("v") == -1
    try:
        for label, letter_key in (("old_way_by_letter", lambda letter: letter), ("new_way_by_key", paste._letter_key)):
            original = paste._letter_key
            paste._letter_key = letter_key
            try:
                desk.focus(top)
                desk.set_text(edit, "")
                paste.insert(text, s, "notepad.exe")
                typed, _ = desk.wait_text(edit, "", timeout=5)
            finally:
                paste._letter_key = original
            res[label] = {"typed": typed, "ok": typed.strip() == text}
            time.sleep(paste.RESTORE_DELAY + 0.3)
    finally:
        desk.u.ActivateKeyboardLayout(us, 0)


_COM_PROBE = r"""
import json, sys, threading
out = {}
def job():
    try:
        if sys.argv[1] == "fixed":
            sys.coinit_flags = 0
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
t = threading.Thread(target=job); t.start(); t.join()
print(json.dumps(out))
"""


def _uia_probe() -> dict:
    """What the focused text box offers, to tell why the text around the cursor can or can't be read."""
    from hyper_wispr import uia

    def job() -> dict:
        try:
            u, mod = uia._uia()
            el = u.GetFocusedElement()
            text = uia._pattern(el, uia.TEXT_PATTERN, mod.IUIAutomationTextPattern)
            sel = text.GetSelection() if text is not None else None
            two = getattr(mod, "IUIAutomationTextPattern2", None)
            return {"class": el.CurrentClassName, "control": el.CurrentControlType, "framework": el.CurrentFrameworkId,
                    "text_pattern": text is not None, "selection_ranges": sel.Length if sel is not None else None,
                    "text_pattern2": two is not None and uia._pattern(el, uia.TEXT_PATTERN2, two) is not None,
                    "value_pattern": uia._pattern(el, uia.VALUE_PATTERN, mod.IUIAutomationValuePattern) is not None}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    return uia.run(job, {"error": "timed out"}, timeout=5)


@section("text_box_reading")
def bench_uia(res: dict, desk: Desktop, notepad) -> None:
    """UI Automation: the text around the cursor (context), and what the word learner reads."""
    from hyper_wispr import uia

    # A fresh Python where, as in the app, the UI Automation thread is the first to import comtypes.
    for way in ("before_fix", "fixed"):
        probe = subprocess.run([sys.executable, "-c", _COM_PROBE, way], capture_output=True, text=True, timeout=120)
        res[f"com_setup_{way}"] = json.loads(probe.stdout or "{}") or probe.stderr[-300:]

    top, edit = notepad
    desk.focus(top)
    desk.set_text(edit, "Overnight the patient")
    t = time.perf_counter()
    f = uia.focus(timeout=10)
    res["first_call_ms"] = round(1000 * (time.perf_counter() - t))
    times = []
    for _ in range(10):
        t = time.perf_counter()
        f = uia.focus()
        times.append(time.perf_counter() - t)
    res["focus_ms"] = round(1000 * median(times), 1)
    res["read_ok"] = f.known and f.editable is True and f.before.endswith("Overnight the patient")
    res["focus"] = {"known": f.known, "editable": f.editable, "before": f.before[-40:], "after": f.after}
    res["probe"] = _uia_probe()
    desk.set_text(edit, "Ward round notes. " * 1200)  # about 21,000 characters
    handle = uia.remember_focus()
    times = []
    for _ in range(5):
        t = time.perf_counter()
        got = uia.text_of(handle, timeout=10)
        times.append(time.perf_counter() - t)
    res["learner_read_ms"] = round(1000 * median(times), 1)
    res["learner_read_chars"] = len(got or "")
    uia.forget(handle)
    desk.set_text(edit, "")


@section("shortcuts")
def bench_hotkeys(res: dict, desk: Desktop, notepad) -> None:
    """The real keyboard hook (pynput) with keys sent through Windows, like a person pressing them."""
    from hyper_wispr import hotkeys

    top, _edit = notepad
    desk.focus(top)
    events, names = [], []
    m = hotkeys.HotkeyStateMachine(
        ["ctrl_l", "cmd"], 0.35,
        on_start=lambda: events.append(("start", time.perf_counter())),
        on_stop=lambda: events.append(("stop", time.perf_counter())),
        on_cancel=lambda: events.append(("cancel", time.perf_counter())),
        on_combo=hotkeys.mask_menu_key, command_key="alt_l",
        paste_last=["ctrl_l", "alt_l", "z"], on_paste_last=lambda: events.append(("paste_last", time.perf_counter())),
        medical_keys=["ctrl_l", "alt_l", "m"], on_medical=lambda: events.append(("medical", time.perf_counter())),
    )
    down = m.key_down
    m.key_down = lambda name: (names.append(name), down(name))[1]
    listener = hotkeys.listen(m)
    time.sleep(1.0)

    def wait_for(kind: str, since: float, timeout: float = 2.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            for k, t in events:
                if k == kind and t >= since:
                    return round(1000 * (t - since), 1)
            time.sleep(0.005)
        return None

    try:
        # Hold to talk.
        t = time.perf_counter()
        desk.key(LCTRL)
        desk.key(LWIN)
        res["hold_start_ms"] = wait_for("start", t)
        time.sleep(0.6)
        t = time.perf_counter()
        desk.key(LWIN, up=True)
        desk.key(LCTRL, up=True)
        res["hold_stop_ms"] = wait_for("stop", t)
        time.sleep(0.5)
        # Tap for hands-free, Alt+Tab away and back, then tap again to finish.
        desk.press(LCTRL, LWIN, hold=0.05)
        time.sleep(0.4)
        res["hands_free_on"] = m.state == hotkeys.HANDS_FREE
        desk.press(LALT, TAB, hold=0.1)
        time.sleep(0.5)
        desk.focus(top)
        res["alt_tab_kept_dictation"] = m.state == hotkeys.HANDS_FREE and not m.command
        t = time.perf_counter()
        desk.press(LCTRL, LWIN, hold=0.05)
        res["hands_free_stop_ms"] = wait_for("stop", t)
        res["command_after_alt_tab"] = m.command
        time.sleep(0.5)
        # Esc cancels.
        desk.key(LCTRL)
        desk.key(LWIN)
        time.sleep(0.5)
        t = time.perf_counter()
        desk.press(ESC)
        res["esc_cancel_ms"] = wait_for("cancel", t)
        desk.key(LWIN, up=True)
        desk.key(LCTRL, up=True)
        time.sleep(0.8)
        # Ctrl+Esc is also Windows' own shortcut for the Start menu.
        res["window_after_esc"] = desk.class_name(desk.u.GetForegroundWindow())
        desk.focus(top)
        # Ctrl+Alt+Z (paste last) and Ctrl+Alt+M (medical on/off).
        names.clear()
        t = time.perf_counter()
        desk.press(LCTRL, LALT, 0x5A)
        res["paste_last_ms"] = wait_for("paste_last", t)
        res["paste_last_key_names"] = list(names)
        names.clear()
        t = time.perf_counter()
        desk.press(LCTRL, LALT, 0x4D)
        res["medical_toggle_ms"] = wait_for("medical", t)
        res["medical_toggle_key_names"] = list(names)
    finally:
        listener.stop()
        for vk in (LWIN, LCTRL, LALT):
            desk.key(vk, up=True)
    res["events"] = [k for k, _ in events]


@section("settings_window")
def bench_window(res: dict) -> None:
    t = time.perf_counter()
    p = subprocess.run([sys.executable, "-m", "hyper_wispr", "--settings-self-test"], cwd=ROOT, timeout=240,
                       capture_output=True, text=True)
    res["self_test_s"] = round(time.perf_counter() - t, 1)
    res["self_test_ok"] = p.returncode == 0
    proc = subprocess.Popen([sys.executable, "-m", "hyper_wispr", "--settings"], cwd=ROOT)
    try:
        with Meter(lambda: tree(proc.pid), every=0.25) as m:
            time.sleep(20)
        res["open_20s"] = m.dict()
        res["processes"] = len(tree(proc.pid))
        with Meter(lambda: tree(proc.pid), every=0.5) as m:
            time.sleep(15)
        res["idle_15s"] = m.dict()
    finally:
        for p in reversed(tree(proc.pid)):
            try:
                p.kill()
            except psutil.Error:
                pass


# ---- the whole app ------------------------------------------------------------------


def app_driver(ctl: Path) -> None:
    """--app-driver: the real tray app, with the test clip named in next.txt as the microphone."""
    from hyper_wispr import __main__, audio
    from hyper_wispr import app as app_module

    class BenchRecorder(audio.Recorder):
        def start(self) -> None:
            with self._lock:
                self._recording = True

        def stop(self) -> np.ndarray:
            with self._lock:
                self._recording = False
            return np.load(ctl / ((ctl / "next.txt").read_text().strip() + ".npy"))

        def level(self) -> float:
            return 0.04

        def open_standby(self) -> None:
            pass

        def close(self) -> None:
            pass

        def device_name(self) -> str:
            return "benchmark clips"

    app_module.Recorder = BenchRecorder
    start_recording = app_module.App.start_recording

    def start_and_note_focus(self) -> None:  # what the app read from the text box (sizes only)
        start_recording(self)
        f = self.focus
        with (ctl / "focus.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"known": f.known, "editable": f.editable, "before_chars": len(f.before)}) + "\n")

    app_module.App.start_recording = start_and_note_focus
    sys.argv = ["hyper-wispr", "--minimized"]
    __main__.main()


@section("app")
def bench_app(res: dict, clips: dict, desk: Desktop, notepad) -> None:
    """The installed app's whole path: shortcut, "recording", speech model, medical fixes, AI cleanup,
    paste into Notepad. Timed from letting go of the keys to the text appearing."""
    top, edit = notepad
    ctl = Path(tempfile.mkdtemp(prefix="hw-bench-"))
    for name in ("short_0", "m2_0", "m6_0", "general_0", "command_0"):
        np.save(ctl / f"{name}.npy", clips[name])
    settings_path = config.settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"updates": {"check_automatically": False}}), encoding="utf-8")
    log_path = config.app_dir() / "hyper-wispr.log"
    start = log_path.stat().st_size if log_path.exists() else 0

    def new_log() -> str:
        try:
            with log_path.open("rb") as f:
                f.seek(start)
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def wait_log(needle: str, timeout: float) -> float | None:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            if needle in new_log():
                return time.perf_counter() - t0
            time.sleep(0.1)
        return None

    t0 = time.perf_counter()
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--app-driver", str(ctl)], cwd=ROOT)
    app = lambda: tree(proc.pid)  # noqa: E731
    try:
        ready = wait_log("Ready:", 600)
        res["start_to_ready_s"] = round(ready + 0, 1) if ready is not None else None
        res["ai_ready_s"] = wait_log("AI cleanup ready", 180)
        time.sleep(3)
        with Meter(app, every=0.5) as m:
            time.sleep(30)
        res["idle_30s"] = m.dict()

        def dictate(clip: str, how: str = "hold", setup: str = "", timeout: float = 90) -> dict:
            (ctl / "next.txt").write_text(clip)
            ok = desk.focus(top)
            desk.set_text(edit, setup, select_all=bool(setup))
            time.sleep(0.3)
            if how == "hold":
                desk.press(LCTRL, LWIN, hold=0.9)
            elif how == "command":
                desk.press(LALT, LCTRL, LWIN, hold=0.9)
            elif how == "tap":
                desk.press(LCTRL, LWIN, hold=0.05)
                time.sleep(1.5)
                desk.press(LCTRL, LWIN, hold=0.05)
            t = time.perf_counter()
            with Meter(app, every=0.1) as mm:
                typed, first = desk.wait_text(edit, setup, timeout=timeout)
            out = {"clip": clip, "how": how, "focused": ok, "typed": typed,
                   "seconds_after_keys_up": round(first, 2) if first is not None else None, "app": mm.dict()}
            ref = reference(clip)
            if ref and how != "command":
                out["wer"] = wer(ref, typed)
                out["terms_missing"] = terms_found(clip, typed)
                out["doses_missing"] = written_missing(clip, typed)
            print(f"  {clip} ({how}): {out['seconds_after_keys_up']}s -> {typed[:120]!r}", flush=True)
            time.sleep(1.0)
            return out

        runs = [dictate("short_0"), dictate("m2_0"), dictate("m6_0"), dictate("general_0", "tap")]
        # Esc while holding the keys: nothing may be typed.
        (ctl / "next.txt").write_text("m2_0")
        desk.focus(top)
        desk.set_text(edit, "")
        desk.key(LCTRL)
        desk.key(LWIN)
        time.sleep(0.6)
        desk.press(ESC)
        desk.key(LWIN, up=True)
        desk.key(LCTRL, up=True)
        typed, _ = desk.wait_text(edit, "", timeout=6)
        runs.append({"clip": "m2_0", "how": "esc", "typed": typed, "nothing_typed": typed == "",
                     "window_after": desk.class_name(desk.u.GetForegroundWindow())})
        # Paste last: Ctrl+Alt+Z types the last dictation again.
        desk.focus(top)
        desk.set_text(edit, "")
        desk.press(LCTRL, LALT, 0x5A)
        typed, first = desk.wait_text(edit, "", timeout=6)
        runs.append({"how": "paste_last", "typed": typed, "seconds": round(first, 2) if first is not None else None})
        # Command mode: select a sentence, hold Alt too, say what to do with it.
        runs.append(dictate("command_0", "command", setup=CLIPS["m3"]))
        # Medical dictation off with Ctrl+Alt+M, the same sentence again, then on again.
        desk.focus(top)
        desk.press(LCTRL, LALT, 0x4D)
        res["medical_shortcut_worked"] = wait_log("Medical dictation off", 5) is not None
        runs.append(dictate("m2_0") | {"medical": False})
        desk.press(LCTRL, LALT, 0x4D)
        wait_log("Medical dictation on", 5)
        # AI cleanup off (as if switched off in Settings): the same sentence again.
        config.update({"ai_cleanup.enabled": False})
        res["settings_reloaded"] = wait_log("Settings reloaded", 10) is not None
        runs.append(dictate("m2_0") | {"ai": False})
        runs.append(dictate("m6_0") | {"ai": False})
        res["runs"] = runs
        focus_log = ctl / "focus.jsonl"
        res["text_box_read_by_app"] = [json.loads(x) for x in focus_log.read_text().splitlines()] if focus_log.exists() else []
        # Idle again, while the word learner watches the last text box for a minute.
        with Meter(app, every=0.5) as m:
            time.sleep(30)
        res["idle_after_30s"] = m.dict()
    finally:
        for p in reversed(app()):
            try:
                p.kill()
            except psutil.Error:
                pass
        # The app's log has no dictated text, only lengths and timings.
        lines = new_log().splitlines()
        res["log"] = [line[:300] for line in lines if " INFO " in line or " WARNING " in line or " ERROR " in line][-80:]


# ---- the report ---------------------------------------------------------------------


def summary() -> str:
    """A short Markdown table of the main numbers, for the job's summary page."""
    out = ["# Hyper-Wispr benchmark", ""]
    env = R.get("environment", {})
    out.append(f"{env.get('cpu')} ({env.get('cores_physical')} cores, {env.get('cores_logical')} threads), "
               f"{env.get('ram_gb')} GB RAM, {env.get('os')}")
    out.append("")
    for name, part in R.items():
        if isinstance(part, dict) and part.get("error"):
            out.append(f"- **{name} failed**: {part['error']}")
    for model in ("speech_small", "speech_turbo"):
        part = R.get(model, {})
        rows = part.get("rows") or []
        if rows:
            out += ["", f"## {part.get('model')}: load {part.get('load', {}).get('seconds')} s, +{part.get('memory_mb')} MB", "",
                    "| clip | audio s | took s | x real time | cores | WER | missing terms | final |", "|---|---|---|---|---|---|---|---|"]
            for r in rows:
                tag = ",".join(k for k in ("noisy", "beam", "languages", "whisper_mode") if k in r) or ("med" if r.get("medical") else "plain")
                out.append(f"| {r['clip']} {tag} | {r['audio_s']} | {r['seconds']} | {r.get('speed')} | {r['cores_busy']} | "
                           f"{r.get('wer', '')} | {', '.join(r.get('terms_missing_final', []))} | {r['final'][:80]} |")
    ai = R.get("ai_cleanup", {})
    if ai.get("rows"):
        out += ["", f"## AI cleanup ({ai.get('model')}): warm-up {ai.get('warm_up', {}).get('seconds')} s, "
                f"{ai.get('words_per_second')} words/s", "",
                "| clip | words | app s | AI used | new: markdown, added route, meaning kept | old: the same | new output |",
                "|---|---|---|---|---|---|---|"]
        for r in ai["rows"]:
            n, o = r["new"], r["old"]
            out.append(f"| {r['clip']} | {r['words']} | {r['app']['seconds']} | {r['app_used_ai']} | "
                       f"{n['markdown']}, {n['added_route']}, {n.get('meaning_kept')} | "
                       f"{o['markdown']}, {o['added_route']}, {o.get('meaning_kept')} | {n['output'][:80]!r} |")
        for key in ("after_idle_no_warm_up", "after_idle_with_warm_up"):
            if key in ai:
                out.append(f"- {key}: {ai[key]['seconds']} s, AI used: {ai[key]['used_ai']}")
    for run in R.get("ai_builtin", {}).get("runs", []):
        out += ["", f"## Built-in AI {run['model']}, guessing ahead {run['draft']}: warm-up {run['warm_up'].get('seconds')} s", "",
                "| clip | words | app s | AI used | numbers, meaning kept | engine | output |", "|---|---|---|---|---|---|---|"]
        for r in run["rows"]:
            out.append(f"| {r['clip']} | {r['words']} | {r['app']['seconds']} | {r['used_ai']} | {r['numbers_kept']}, "
                       f"{r['meaning_kept']} | {r.get('engine')} | {r['output'][:80]!r} |")
    app = R.get("app", {})
    if app.get("runs"):
        out += ["", f"## Whole app: ready {app.get('start_to_ready_s')} s, idle {app.get('idle_30s')}", "",
                "| clip | how | s after keys up | typed |", "|---|---|---|---|"]
        for r in app["runs"]:
            out.append(f"| {r.get('clip', '')} | {r.get('how')} | {r.get('seconds_after_keys_up', r.get('seconds', ''))} | "
                       f"{(r.get('typed') or '')[:90]!r} |")
    return "\n".join(out) + "\n"


def main() -> None:
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench-results")
    parser.add_argument("--app-driver", metavar="FOLDER")
    parser.add_argument("--skip", default="", help="comma-separated parts to leave out")
    args = parser.parse_args()
    if args.app_driver:
        app_driver(Path(args.app_driver))
        return
    OUT = Path(args.out)
    skip = set(filter(None, args.skip.split(",")))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    work = Path(tempfile.mkdtemp(prefix="hw-bench-"))
    environment()
    clips = make_audio(work / "audio") or {}
    if "medical" not in skip:
        bench_medical()
    if "history" not in skip:
        bench_history(work / "history")
    if "speech" not in skip:
        bench_speech(clips)
    if "ai" not in skip:
        # The sentences as written, not as heard, so runs can be compared.
        bench_ai({k: CLIPS[k.split("_")[0]] for k in AI_CLIPS})
    if "ai_builtin" not in skip:
        bench_ai_builtin({k: CLIPS[k.split("_")[0]] for k in AI_CLIPS})
    if "turbo" not in skip:
        bench_turbo(clips)
    if "overlay" not in skip:
        bench_overlay()
    if sys.platform == "win32" and "desktop" not in skip:
        desk = Desktop()
        pad = subprocess.Popen(["notepad.exe"])
        try:
            notepad = desk.notepad(pad.pid)
            R["desktop"] = {"notepad": [desk.class_name(h) for h in notepad], "focused": desk.focus(notepad[0])}
            bench_paste(desk, notepad)
            bench_uia(desk, notepad)
            bench_hotkeys(desk, notepad)
            if "app" not in skip:
                bench_app(clips, desk, notepad)
        except Exception as e:
            R["desktop_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            pad.kill()
    if "window" not in skip:
        bench_window()
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save()
    (OUT / "summary.md").write_text(summary(), encoding="utf-8")
    print("\n===== RESULTS JSON =====")
    print(json.dumps(R, default=str))
    print("===== END =====")


if __name__ == "__main__":
    main()
