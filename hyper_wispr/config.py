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
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Hold these keys to talk; release to transcribe and paste.
    # Names follow pynput: ctrl, ctrl_l, ctrl_r, cmd (Windows key), alt, shift, space, f1..f24.
    "push_to_talk_keys": ["ctrl_l", "cmd"],
    # Tap the push-to-talk keys (shorter than this) to start hands-free mode; tap again to stop.
    "hands_free_tap_seconds": 0.35,
    # Command mode: select some text, hold the push-to-talk keys plus this key, and say what to
    # do with it ("make this shorter", "turn this into a bulleted list"). Needs AI cleanup.
    "command_mode": {"enabled": True, "key": "alt_l"},
    # Pastes your last dictation again wherever the cursor is. (Not Alt+Shift: that switches
    # the keyboard language when Arabic is installed.)
    "paste_last_keys": ["ctrl_l", "alt_l", "z"],
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
        # For speaking quietly (next to a patient, in an open office): picks up much softer speech.
        "whisper_mode": False,
        # Mute music and videos while dictating (never calls), and unmute them afterwards.
        "mute_media": True,
        # Keep the microphone open all the time, so the first word is never cut off. Windows then
        # shows the microphone as in use whenever Hyper-Wispr is running.
        "keep_mic_open": False,
    },
    "cleanup": {
        "remove_fillers": True,
        # Only sounds that are never real words: "er" would delete "ER" and "mm" would delete millimetres.
        "filler_words": ["um", "umm", "uh", "uhh", "uhm", "erm", "ehm", "ah", "hmm", "hm", "mmm"],
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
        # Tone per kind of app: "formal", "casual" or "very_casual".
        "styles": {"email": "formal", "message": "casual", "document": "formal", "ai_chat": "casual", "code": "formal", "other": "formal"},
    },
    # Read the text around the cursor so a dictation continues your sentence (capitals, spacing)
    # and the AI can spell names already on screen. Read locally, never stored or logged.
    "context_awareness": True,
    # Offer words you correct after a dictation for the dictionary.
    "learn_corrections": True,
    # Words Whisper should spell your way: names, drugs, jargon.
    "dictionary": [],
    # Dictionary words that matter most: always given to the speech model first.
    "dictionary_starred": [],
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
    "history": {
        "enabled": True,
        # Delete dictations older than this many days (0 = keep them all).
        "keep_days": 30,
        # Keep the recordings of the last few dictations so they can be redone with a bigger model.
        "keep_recordings": 20,
        "redo_model": "large-v3-turbo",
    },
    # The floating "Listening…" pill while you dictate. position: "bottom" or "top" of the screen.
    "overlay": {"enabled": True, "position": "bottom"},
    # Look for a newer version on GitHub (at most once a day, when the window opens).
    "updates": {"check_automatically": True},
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


# Filler list shipped up to v0.2. Files that still have exactly this list get the safer one above.
_OLD_FILLER_WORDS = ["um", "uh", "erm", "er", "ah", "hmm", "mm", "uhm"]
# Keys earlier versions wrote that no longer do anything.
_OBSOLETE_KEYS = ("hands_free_extra_key",)


def migrate(settings: dict[str, Any]) -> dict[str, Any]:
    """Update settings written by older versions (in place) and return them."""
    for key in _OBSOLETE_KEYS:
        settings.pop(key, None)
    if isinstance(settings.get("history"), dict):
        settings["history"].pop("keep_audio", None)  # never did anything; recordings are keep_recordings
    cleanup = settings.get("cleanup")
    if isinstance(cleanup, dict) and cleanup.get("filler_words") == _OLD_FILLER_WORDS:
        cleanup["filler_words"] = list(DEFAULTS["cleanup"]["filler_words"])
    return settings


def load(path: Path | None = None) -> dict[str, Any]:
    """Raises ValueError if the file isn't valid settings JSON (e.g. a typo from editing it by hand)."""
    path = path or settings_path()
    if not path.exists():
        save(DEFAULTS, path)
        return copy.deepcopy(DEFAULTS)
    # utf-8-sig: Notepad can save with a byte-order mark, which plain utf-8 JSON parsing rejects.
    with path.open(encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings.json must contain a JSON object")
    return migrate(merge(DEFAULTS, data))


def recover(path: Path | None = None) -> Path:
    """Move an unreadable settings file aside and start again from the defaults. Returns the backup."""
    path = path or settings_path()
    backup = path.with_name("settings.broken.json")
    _replace(path, backup)
    save(DEFAULTS, path)
    return backup


def update(changes: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Apply {"section.key": value} changes to the file as it is now, so settings changed elsewhere
    meanwhile (the tray menu, a hand edit, another window) are kept. Returns the saved settings."""
    path = path or settings_path()
    try:
        settings = load(path)
    except ValueError:
        recover(path)  # unreadable file: keep it as settings.broken.json and start from the defaults
        settings = copy.deepcopy(DEFAULTS)
    for dotted, value in changes.items():
        *parents, last = dotted.split(".")
        node = settings
        for key in parents:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[last] = value
    save(settings, path)
    return settings


# Settings whose keys are the user's own words: changed as a whole, never key by key.
_WHOLE_VALUES = {"replacements", "snippets"}


def diff(base: dict[str, Any], new: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """{"section.key": value} for everything that differs between two versions, for update()."""
    changes: dict[str, Any] = {}
    for key, value in new.items():
        path = prefix + key
        old = base.get(key) if isinstance(base, dict) else None
        if isinstance(value, dict) and isinstance(old, dict) and path not in _WHOLE_VALUES:
            changes.update(diff(old, value, path + "."))
        elif value != old or (isinstance(base, dict) and key not in base):
            changes[path] = value
    return changes


def save(settings: dict[str, Any], path: Path | None = None) -> None:
    path = path or settings_path()
    # Write a temp file and swap it in, so the running app never reads half a file. The temp name is
    # unique because the tray app and the Settings window can both save at the same moment.
    fd, tmp = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        _replace(Path(tmp), path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _replace(src: Path, dst: Path) -> None:
    # On Windows the swap fails while another process (the other Hyper-Wispr window, an antivirus
    # scan) has the file open for a moment, so try again briefly before giving up.
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
