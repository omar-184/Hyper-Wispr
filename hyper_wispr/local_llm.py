"""The AI cleanup model run by Hyper-Wispr itself (llama.cpp), so it needs no Ollama.

The model is a GGUF file downloaded once on first start, like the speech model, then everything is
offline. It runs in a helper process (the same program started with --ai-worker): if the model ever
crashes or runs out of memory, only the helper goes and dictation keeps working with the basic cleanup.

Two things make it faster than the same model in Ollama on a processor:
- The instructions, which are the same for every dictation, are read once and kept (Ollama does
  this too), so a dictation only costs its own words.
- Guessing ahead from the dictation ("prompt lookup"): a cleaned text is mostly the dictated words
  again, so the next few words are guessed from the dictation and the model checks all of them in
  one step, which on a processor takes little more than writing one word. Only guesses the model
  itself would have written are kept, so the text is exactly what it would write one word at a time.
"""

from __future__ import annotations

import gc
import logging
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Downloaded from Hugging Face the first time, from the model makers' own repositories.
MODELS: dict[str, dict[str, Any]] = {
    "qwen2.5-1.5b": {
        "label": "Fast (Qwen 2.5 1.5B, about 1 GB)",
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    },
    "qwen2.5-3b": {
        "label": "Better (Qwen 2.5 3B, about 2 GB)",
        "repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "file": "qwen2.5-3b-instruct-q4_k_m.gguf",
    },
}
# Measured on the 4-thread benchmark machine with medical dictation (2026-10-25): a 110-word note took
# 12 s with the 1.5B model and 26 s with the 3B one (30 s in Ollama), and the 3B model's answer to it
# left out a sentence in 3 of 4 runs, so the medical check pasted the basic cleanup instead. The 1.5B
# model passed every medical check.
DEFAULT_MODEL = "qwen2.5-1.5b"
N_CTX = 4096
IDLE_UNLOAD = 30 * 60  # the helper lets the model go after this long without a dictation, like Ollama


# ---- can this PC run it --------------------------------------------------------------


def cpu_supported() -> bool:
    """The built-in engine is compiled for processors with AVX2 (Intel 2013 and newer, AMD 2015 and
    newer). An older one would crash the helper, so it uses Ollama instead."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE))
    except Exception:
        return True


def installed() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("llama_cpp") is not None
    except (ImportError, ValueError):
        return False


def why_not() -> str:
    """Why the built-in engine can't run on this PC, or "" if it can."""
    if not installed():
        return "this copy of Hyper-Wispr was installed without the built-in AI engine"
    if not cpu_supported():
        return "this processor is too old for the built-in AI engine (it needs AVX2)"
    return ""


def usable() -> bool:
    return not why_not()


def engine(cfg: dict[str, Any]) -> str:
    """"builtin" or "ollama": the one AI cleanup uses. "builtin" falls back to Ollama on PCs that can't run it."""
    if cfg.get("engine", "builtin") == "ollama" or not usable():
        return "ollama"
    return "builtin"


# ---- the model file --------------------------------------------------------------------


def model_info(key: str) -> dict[str, Any]:
    return MODELS.get(key) or MODELS[DEFAULT_MODEL]


def model_path(key: str) -> str | None:
    """The model file if it's downloaded, else None. A path to a .gguf file of your own also works."""
    if key.lower().endswith(".gguf"):
        return key if os.path.isfile(key) else None
    import huggingface_hub

    m = model_info(key)
    try:
        return huggingface_hub.hf_hub_download(m["repo"], m["file"], local_files_only=True)
    except Exception:
        return None


def download(key: str, progress=None) -> str:
    """Download the model the first time and return its path. progress(done_bytes, total_bytes or 0, "ai")
    is called about twice a second while downloading."""
    if path := model_path(key):
        return path
    if key.lower().endswith(".gguf"):
        raise FileNotFoundError(key)
    import huggingface_hub

    from .speech import _folder_size

    m = model_info(key)
    log.info("Downloading AI cleanup model %s (first time only)", m["file"])
    total = 0
    try:
        info = huggingface_hub.HfApi().model_info(m["repo"], files_metadata=True)
        total = sum(f.size or 0 for f in info.siblings or [] if f.rfilename == m["file"])
    except Exception:
        log.info("Couldn't get the AI model's download size", exc_info=True)
    folder = Path(huggingface_hub.constants.HF_HUB_CACHE) / ("models--" + m["repo"].replace("/", "--"))
    before = _folder_size(folder)  # the other model of the same maker may be in there already
    done = threading.Event()

    def report() -> None:
        while not done.wait(0.5):
            if progress is not None:
                progress(max(0, _folder_size(folder) - before), total, "ai")

    threading.Thread(target=report, daemon=True).start()
    try:
        path = huggingface_hub.hf_hub_download(m["repo"], m["file"])
    finally:
        done.set()
    if progress is not None:
        progress(total, total, "ai")
    log.info("AI cleanup model downloaded")
    return path


# ---- the engine (runs in the helper process) ------------------------------------------------


def lookup(tokens: list[int], source: list[int], start: int, n: int, max_ngram: int = 3) -> tuple[list[int], int]:
    """Guess the next n tokens of `tokens` from `source` (the dictation): find where its last few
    tokens appear in the source, preferring the first place at or after `start` (where the last
    guess came from: a cleanup goes through the dictation in order), and return what follows there
    and where that is in the source."""
    for size in range(min(max_ngram, len(tokens)), 0, -1):
        tail = tokens[-size:]
        best = -1
        for i in range(len(source) - size):
            if source[i : i + size] == tail:
                best = i
                if i + size >= start:
                    break
        if best >= 0:
            at = best + size
            return source[at : at + n], at
    return [], start


class Engine:
    """A loaded model and what it has already read (llama.cpp's cache), answering one request at a time."""

    def __init__(self, path: str, n_ctx: int = N_CTX, n_threads: int | None = None):
        import llama_cpp
        from llama_cpp import Llama

        self.llama_cpp = llama_cpp
        t = time.monotonic()
        self.llm = Llama(model_path=path, n_ctx=n_ctx, n_batch=512, n_threads=n_threads, verbose=False)
        self.load_seconds = time.monotonic() - t
        self.n_ctx = n_ctx
        self.n_vocab = self.llm.n_vocab()
        self.vocab = llama_cpp.llama_model_get_vocab(self.llm.model)
        self.cached: list[int] = []  # the tokens whose keys and values are in llama.cpp's cache
        self.formatter = self._formatter()

    def _formatter(self):
        """The model's own chat format, from its file (Qwen's is ChatML)."""
        from llama_cpp import llama_chat_format

        meta = self.llm.metadata
        template = meta.get("tokenizer.chat_template")
        if not template:
            return None

        def text(key: str) -> str:
            try:
                return self.llm._model.token_get_text(int(meta[key]))
            except Exception:
                return ""

        return llama_chat_format.Jinja2ChatFormatter(
            template=template, eos_token=text("tokenizer.ggml.eos_token_id"), bos_token=text("tokenizer.ggml.bos_token_id")
        )

    def prompt(self, system: str, user: str) -> list[int]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.formatter is not None:
            text = self.formatter(messages=messages).prompt
        else:  # ChatML
            text = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages) + "<|im_start|>assistant\n"
        bos = self.llm._model.token_bos()
        tokens = self.llm.tokenize(text.encode("utf-8"), add_bos=False, special=True)
        if self.llm._model.add_bos_token() and bos >= 0 and (not tokens or tokens[0] != bos):
            tokens = [bos] + tokens
        return tokens

    # -- the model's cache --

    def _keep(self, n: int) -> None:
        """Forget everything after the first n tokens."""
        self.llm._ctx.kv_cache_seq_rm(0, n, -1)
        del self.cached[n:]

    def _decode(self, tokens: list[int], logits_all: bool) -> None:
        batch = self.llm._batch
        batch.set_batch(batch=tokens, n_past=len(self.cached), logits_all=logits_all)
        self.llm._ctx.decode(batch)
        self.cached += tokens

    def _best(self, i: int) -> int:
        import numpy as np

        logits = np.ctypeslib.as_array(self.llm._ctx.get_logits_ith(i), shape=(self.n_vocab,))
        return int(logits.argmax())

    def _is_end(self, token: int) -> bool:
        return bool(self.llama_cpp.llama_vocab_is_eog(self.vocab, token))

    def chat(self, system: str, user: str, max_tokens: int = 512, first_timeout: float = 60.0,
             budget: float | None = None, draft: int = 8, source: str | None = None,
             start: float | None = None) -> dict[str, Any]:
        """Greedy (temperature 0) answer. first_timeout: the longest wait before writing starts;
        budget: the longest time in all, both counted from `start` (default: now). source: the text
        to guess ahead from (the dictation); draft: how many tokens to guess at a time (0: one at a time)."""
        start = time.monotonic() if start is None else start
        tokens = self.prompt(system, user)
        if len(tokens) + 8 > self.n_ctx:
            raise ValueError(f"the request is too long ({len(tokens)} tokens)")
        max_tokens = min(max_tokens, self.n_ctx - len(tokens) - 1)
        # Read only what differs from the last request: the instructions are usually the same.
        same = 0
        for a, b in zip(self.cached, tokens):
            if a != b:
                break
            same += 1
        same = min(same, len(tokens) - 1)  # the last token is read again for its prediction
        self._keep(same)
        rest = tokens[same:]
        n_batch = self.llm.n_batch
        for i in range(0, len(rest), n_batch):
            if time.monotonic() - start > first_timeout:
                self._keep(0)  # half-read: start over next time
                raise TimeoutError(f"the AI didn't start within {first_timeout:.0f} s")
            self._decode(rest[i : i + n_batch], logits_all=False)
        prompt_seconds = time.monotonic() - start
        nxt = self._best(-1)
        src = self.llm.tokenize(source.encode("utf-8"), add_bos=False, special=False) if source and draft else []
        out: list[int] = []
        guessed = accepted = steps = 0
        pos = 0  # where in the dictation the cleanup has got to
        while not self._is_end(nxt) and len(out) < max_tokens:
            out.append(nxt)
            if budget is not None and time.monotonic() - start > budget:
                raise TimeoutError(f"no complete answer within {budget:.0f} s")
            guess, at = lookup(out, src, pos, min(draft, max_tokens - len(out))) if src else ([], pos)
            guess = guess[: max(0, self.n_ctx - len(self.cached) - 2)]
            self._decode([nxt] + guess, logits_all=True)
            steps += 1
            guessed += len(guess)
            # Keep each guess the model agrees with, up to the first it doesn't.
            n_ok, done = 0, False
            nxt = self._best(0)
            for g in guess:
                if nxt != g:
                    break
                n_ok += 1
                if self._is_end(g) or len(out) >= max_tokens:
                    done = True
                    break
                out.append(g)
                nxt = self._best(n_ok)
            if n_ok < len(guess):
                self._keep(len(self.cached) - (len(guess) - n_ok))
            accepted += n_ok
            if guess:
                pos = at + n_ok
            if done:
                break
        text = self.llm.detokenize(out).decode("utf-8", errors="ignore")
        total = time.monotonic() - start
        return {
            "text": text,
            "stats": {
                "prompt_tokens": len(tokens), "reused_tokens": same, "prompt_s": round(prompt_seconds, 2),
                "output_tokens": len(out), "output_s": round(total - prompt_seconds, 2), "steps": steps,
                "guessed": guessed, "guesses_kept": accepted, "seconds": round(total, 2),
            },
        }


# ---- the helper process ------------------------------------------------------------------------


def worker_main(address: str) -> int:
    """`Hyper-Wispr --ai-worker ADDRESS`: serve requests from the app until it goes away."""
    from multiprocessing.connection import Listener

    key = bytes.fromhex(os.environ.pop("HYPER_WISPR_AI_KEY", ""))
    if not key:
        return 2
    family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
    eng: Engine | None = None
    loaded = ""
    with Listener(address, family=family, authkey=key) as listener:
        with listener.accept() as conn:
            while True:
                # Let the model go after a while without dictations, to give its memory back.
                if not conn.poll(IDLE_UNLOAD if eng is not None else None):
                    log.info("AI helper: no dictation for %d minutes, letting the model go", IDLE_UNLOAD // 60)
                    eng = None
                    gc.collect()
                    continue
                try:
                    req = conn.recv()
                except (EOFError, OSError):
                    return 0  # the app closed
                received = time.monotonic()  # loading the model counts toward the time limits
                try:
                    op = req.get("op")
                    if op == "quit":
                        return 0
                    if eng is None or loaded != req["path"]:
                        eng = None
                        gc.collect()
                        eng = Engine(req["path"], n_threads=req.get("threads"))
                        loaded = req["path"]
                        log.info("AI helper: model loaded in %.1f s", eng.load_seconds)
                    if op == "load":
                        reply: dict[str, Any] = {"ok": True}
                    else:
                        reply = {"ok": True, **eng.chat(req["system"], req["user"], req.get("max_tokens", 512),
                                                         req.get("first_timeout", 60), req.get("budget"),
                                                         req.get("draft", 8), req.get("source"), received)}
                except Exception as e:
                    log.warning("AI helper: %s: %s", type(e).__name__, e)
                    reply = {"ok": False, "error": f"{type(e).__name__}: {e}", "timeout": isinstance(e, TimeoutError)}
                try:
                    conn.send(reply)
                except (EOFError, OSError):
                    return 0


class _Helper:
    """The app's side: starts the helper process when needed and talks to it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.conn = None

    def _start(self) -> None:
        from multiprocessing.connection import Client

        key = secrets.token_bytes(32)
        if sys.platform == "win32":
            address = rf"\\.\pipe\hyper-wispr-ai-{os.getpid()}-{secrets.token_hex(4)}"
        else:
            address = os.path.join(tempfile.gettempdir(), f"hyper-wispr-ai-{os.getpid()}-{secrets.token_hex(4)}")
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--ai-worker", address]
        else:
            cmd = [sys.executable, "-m", "hyper_wispr", "--ai-worker", address]
        env = {**os.environ, "HYPER_WISPR_AI_KEY": key.hex()}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, env=env, creationflags=flags, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while True:
            try:
                self.conn = Client(address, family="AF_PIPE" if sys.platform == "win32" else "AF_UNIX", authkey=key)
                return
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    self._stop()
                    raise RuntimeError("the AI helper didn't start")
                time.sleep(0.1)

    def _stop(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None

    def request(self, req: dict[str, Any], wait: float, lock_wait: float) -> dict[str, Any]:
        """wait: the longest time for the answer; past it the helper is stuck and is restarted."""
        if not self.lock.acquire(timeout=lock_wait):
            raise TimeoutError("the AI is busy with another request")
        try:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    log.warning("The AI helper stopped (exit code %s); starting it again", self.proc.returncode)
                self._stop()
                self._start()
            try:
                self.conn.send(req)
                if not self.conn.poll(wait):
                    self._stop()
                    raise TimeoutError(f"the AI helper didn't answer within {wait:.0f} s")
                reply = self.conn.recv()
            except (EOFError, OSError) as e:
                self._stop()
                raise RuntimeError(f"the AI helper stopped: {e}") from e
        finally:
            self.lock.release()
        if not reply.get("ok"):
            raise (TimeoutError if reply.get("timeout") else RuntimeError)(reply.get("error", "the AI failed"))
        return reply

    def close(self) -> None:
        with self.lock:
            if self.conn is not None:
                try:
                    self.conn.send({"op": "quit"})
                except OSError:
                    pass
            self._stop()


_helper = _Helper()


def _path(cfg: dict[str, Any]) -> str:
    key = cfg.get("builtin_model") or DEFAULT_MODEL
    path = model_path(key)
    if path is None:
        raise FileNotFoundError(f"the AI model {key} isn't downloaded yet")
    return path


def load(cfg: dict[str, Any], wait: float = 180) -> None:
    _helper.request({"op": "load", "path": _path(cfg)}, wait, lock_wait=wait)


def chat(cfg: dict[str, Any], system: str, user: str, timeout: float, budget: float | None = None,
         max_tokens: int = 512, source: str | None = None) -> str:
    """timeout: the longest wait for the AI to start writing (loading the model doesn't count when it's
    already loaded); budget: the longest time in all."""
    req = {"op": "chat", "path": _path(cfg), "system": system, "user": user, "max_tokens": max_tokens,
           "first_timeout": timeout, "budget": budget, "draft": int(cfg.get("draft_tokens", 8)), "source": source}
    reply = _helper.request(req, (budget or timeout) + 15, lock_wait=timeout)
    stats = reply.get("stats") or {}
    log.info("AI cleanup (built in): %s", ", ".join(f"{k} {v}" for k, v in stats.items()))
    return reply["text"]


def close() -> None:
    _helper.close()
