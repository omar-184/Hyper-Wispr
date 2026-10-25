"""Mute music and videos while you dictate, and unmute them afterwards.

Uses the per-app volume controls of the Windows mixer, so only apps that are playing sound
right now are muted, and only the ones Hyper-Wispr muted are unmuted. Calls are left alone:
muting Teams or Zoom mid-call would cut off the other person.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading

from . import uia

log = logging.getLogger(__name__)

# Voice and video calls: never muted.
CALL_APPS = {
    "teams.exe", "ms-teams.exe", "zoom.exe", "cpthost.exe", "webex.exe", "webexmta.exe", "ciscocollabhost.exe",
    "skype.exe", "slack.exe", "discord.exe", "whatsapp.exe", "telegram.exe", "signal.exe", "lync.exe",
}
ACTIVE = 1  # AudioSessionStateActive

_lock = threading.Lock()
_muted: set[int] = set()  # process ids we muted


def _state_file():
    from .config import app_dir

    return app_dir() / "muted.json"


def _sessions():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetAllSessions()


def _mute_job() -> list[int]:
    uia.init_com()  # runs on the UI Automation thread
    done = []
    for session in _sessions():
        proc = session.Process
        if proc is None or session.ProcessId == os.getpid() or session.State != ACTIVE:
            continue
        if proc.name().lower() in CALL_APPS:
            continue
        volume = session.SimpleAudioVolume
        if not volume.GetMute():
            volume.SetMute(1, None)
            done.append(session.ProcessId)
    return done


def _unmute_job(pids: set[int]) -> None:
    uia.init_com()  # runs on the UI Automation thread
    for session in _sessions():
        if session.ProcessId in pids:
            session.SimpleAudioVolume.SetMute(0, None)


def mute_others() -> None:
    if sys.platform != "win32":
        return
    pids = uia.run(_mute_job, [], timeout=1.0)
    if not pids:
        return
    with _lock:
        _muted.update(pids)
        _save()
    log.info("Muted %d app(s) while dictating", len(pids))


def unmute() -> None:
    with _lock:
        pids = set(_muted)
    if not pids:
        return
    if not uia.run(lambda: _unmute_job(pids) or True, False, timeout=3.0, wait=3.0):
        log.warning("Couldn't unmute the apps muted while dictating; trying again next time")
        return
    with _lock:
        _muted.difference_update(pids)
        _save()


def _save() -> None:
    """Written down so a crash mid-dictation doesn't leave your music muted for good."""
    try:
        path = _state_file()
        if _muted:
            path.write_text(json.dumps(sorted(_muted)))
        else:
            path.unlink(missing_ok=True)
    except OSError:
        log.debug("Couldn't save the muted apps list", exc_info=True)


def recover() -> None:
    """At start-up: unmute anything a previous run muted and never got to unmute."""
    try:
        path = _state_file()
        if not path.exists():
            return
        pids = set(json.loads(path.read_text()))
    except (OSError, ValueError):
        return
    with _lock:
        _muted.update(pids)
    unmute()
