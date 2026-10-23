"""Settings: loaded from a plain JSON file the user can edit by hand.

On Windows the file lives at %APPDATA%\\HyperWispr\\settings.json.
Any key missing from the file falls back to DEFAULTS, so old settings
files keep working when new options are added.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Hold these keys to talk; release to transcribe and paste.
    # Names follow pynput: ctrl, ctrl_l, ctrl_r, cmd (Windows key), alt, shift, space, f1..f24.
    "push_to_talk_keys": ["ctrl_l", "cmd"],
    # Tap the push-to-talk keys (shorter than this) to start hands-free mode; tap again to stop.
    "hands_free_tap_seconds": 0.35,
    "speech": {
        # "auto" picks large-v3-turbo on an NVIDIA GPU and "small" on CPU.
        # Other options: tiny, base, small, medium, large-v3, large-v3-turbo, distil-large-v3,
        # or the *.en English-only variants (tiny.en, base.en, small.en, medium.en).
        "model": "auto",
        "device": "auto",  # auto | cuda | cpu
        "compute_type": "auto",  # auto | float16 | int8_float16 | int8
        # Force one language ("en", "ar", ...). Empty = pick from "languages" below.
        "language": "",
        # Languages you dictate in. With one entry it is always used; with several, the most
        # likely of these is detected each time; empty = any language (least reliable).
        "languages": ["en"],
        "beam_size": 5,
        # Skip silent stretches so Whisper doesn't invent text in long pauses.
        "vad_filter": True,
    },
    "audio": {
        "sample_rate": 16000,
        "input_device": None,  # None = Windows default microphone
        # Recordings shorter than this are treated as accidental key presses.
        "min_seconds": 0.35,
        # Recordings quieter than this (RMS) are treated as "no speech" and nothing is typed.
        "silence_threshold": 0.003,
        "max_seconds": 360,
        "sounds": True,
    },
    "cleanup": {
        "remove_fillers": True,
        "filler_words": ["um", "uh", "erm", "er", "ah", "hmm", "mm", "uhm"],
        # Spoken commands like "new line", "new paragraph", "period".
        "spoken_punctuation": True,
        # "scratch that" deletes the sentence before it; "I mean"/"actually" corrections are left to the AI step.
        "backtrack_phrases": ["scratch that", "delete that"],
    },
    "ai_cleanup": {
        # Uses a local Ollama model (https://ollama.com). Nothing leaves the computer.
        "enabled": True,
        "ollama_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:3b",
        "timeout_seconds": 8,
        # Short dictations (< this many words) skip the AI step to stay instant.
        "min_words": 4,
        # Adds medical-writing rules: keep drug names, doses, units and abbreviations exact.
        "medical_mode": True,
        "extra_instructions": "",
    },
    # Words Whisper should spell your way: names, drugs, jargon.
    "dictionary": [],
    # Always replace the left side with the right side after transcription (case-insensitive).
    "replacements": {},
    # Say the trigger phrase on its own and the expansion is pasted instead.
    "snippets": {},
    "paste": {
        # "clipboard" pastes with Ctrl+V and then restores your clipboard; "type" types characters one by one.
        "method": "clipboard",
        "restore_clipboard": True,
        "add_trailing_space": True,
    },
    "history": {"enabled": True, "keep_audio": False},
}


def app_dir() -> Path:
    base = os.environ.get("APPDATA") if sys.platform == "win32" else None
    root = Path(base) if base else Path.home() / ".config"
    path = root / "HyperWispr"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return app_dir() / "settings.json"


def merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge override onto a copy of defaults."""
    out = copy.deepcopy(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and out[key]:
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or settings_path()
    if not path.exists():
        save(DEFAULTS, path)
        return copy.deepcopy(DEFAULTS)
    with path.open(encoding="utf-8") as f:
        return merge(DEFAULTS, json.load(f))


def save(settings: dict[str, Any], path: Path | None = None) -> None:
    path = path or settings_path()
    # Write a temp file and swap it in, so the running app never reads half a file.
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
